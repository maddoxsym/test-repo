"""Train / validation / out-of-sample splitting with an embargo.

Two rules matter here:

1. **Chronological order is never broken.** Random shuffling of time-series data
   leaks the future into the past; splits are always contiguous and forward.
2. **An embargo gap separates the segments.** Indicators have memory: a 200-bar
   EMA at the first out-of-sample bar was computed from training bars. The
   embargo discards enough bars for that memory to decay, which keeps train/test
   contamination out of the evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..exchange.models import Candle
from ..utils.errors import ConfigError
from ..utils.timeutil import iso, ms_to_dt


@dataclass(frozen=True, slots=True)
class DataSegment:
    """A contiguous, labelled slice of history."""

    label: str
    candles: list[Candle]

    @property
    def start_ms(self) -> int:
        return self.candles[0].open_ms if self.candles else 0

    @property
    def end_ms(self) -> int:
        return self.candles[-1].open_ms if self.candles else 0

    @property
    def start_iso(self) -> str:
        return iso(ms_to_dt(self.start_ms)) if self.candles else ""

    @property
    def end_iso(self) -> str:
        return iso(ms_to_dt(self.end_ms)) if self.candles else ""

    def __len__(self) -> int:
        return len(self.candles)

    def describe(self) -> str:
        return f"{self.label}: {len(self.candles)} bars {self.start_iso} → {self.end_iso}"


@dataclass(frozen=True, slots=True)
class DataSplit:
    """The three evaluation segments."""

    train: DataSegment
    validation: DataSegment
    out_of_sample: DataSegment
    embargo_bars: int

    def describe(self) -> list[str]:
        return [
            self.train.describe(),
            self.validation.describe(),
            self.out_of_sample.describe(),
            f"embargo: {self.embargo_bars} bars between segments",
        ]

    def segments(self) -> dict[str, DataSegment]:
        return {
            "train": self.train,
            "validation": self.validation,
            "oos": self.out_of_sample,
        }


def split_candles(
    candles: list[Candle],
    *,
    train_fraction: float = 0.5,
    validation_fraction: float = 0.2,
    embargo_bars: int = 20,
) -> DataSplit:
    """Split chronologically into train / validation / out-of-sample."""
    if train_fraction <= 0 or validation_fraction <= 0:
        raise ConfigError("train_fraction and validation_fraction must be positive")
    if train_fraction + validation_fraction >= 1.0:
        raise ConfigError(
            "train_fraction + validation_fraction must be < 1.0 to leave out-of-sample data"
        )

    total = len(candles)
    minimum = 3 * (embargo_bars + 1) + 30
    if total < minimum:
        raise ConfigError(
            f"need at least {minimum} candles to split with an embargo of {embargo_bars}; got {total}"
        )

    train_end = int(total * train_fraction)
    validation_end = int(total * (train_fraction + validation_fraction))

    train = candles[:train_end]
    validation = candles[train_end + embargo_bars : validation_end]
    oos = candles[validation_end + embargo_bars :]

    return DataSplit(
        train=DataSegment("train", train),
        validation=DataSegment("validation", validation),
        out_of_sample=DataSegment("oos", oos),
        embargo_bars=embargo_bars,
    )


def split_multi_timeframe(
    candles: dict[str, list[Candle]],
    *,
    primary_timeframe: str,
    train_fraction: float = 0.5,
    validation_fraction: float = 0.2,
    embargo_bars: int = 20,
) -> dict[str, dict[str, list[Candle]]]:
    """Split across timeframes using consistent time boundaries.

    Splitting each timeframe by *index* would misalign them (a 5m series has 12x
    the bars of a 1h series). Boundaries are therefore derived from the primary
    timeframe and applied to the others by **timestamp**.
    """
    primary = candles.get(primary_timeframe, [])
    base = split_candles(
        primary,
        train_fraction=train_fraction,
        validation_fraction=validation_fraction,
        embargo_bars=embargo_bars,
    )

    bounds = {
        "train": (base.train.start_ms, base.train.end_ms),
        "validation": (base.validation.start_ms, base.validation.end_ms),
        "oos": (base.out_of_sample.start_ms, base.out_of_sample.end_ms),
    }

    output: dict[str, dict[str, list[Candle]]] = {}
    for segment_name, (_segment_start_ms, end_ms) in bounds.items():
        segment: dict[str, list[Candle]] = {}
        for timeframe, series in candles.items():
            if timeframe == primary_timeframe:
                segment[timeframe] = getattr(
                    base, {"train": "train", "validation": "validation", "oos": "out_of_sample"}[segment_name]
                ).candles
            else:
                # Context timeframes keep leading history so their indicators are
                # warm at the segment's first bar — that is not leakage, because
                # those bars are strictly in the past.
                segment[timeframe] = [c for c in series if c.open_ms <= end_ms]
        output[segment_name] = segment

    return output


@dataclass(frozen=True, slots=True)
class WalkForwardWindow:
    """One in-sample/out-of-sample pair in a walk-forward analysis."""

    index: int
    train: DataSegment
    test: DataSegment

    @property
    def label(self) -> str:
        return f"wf{self.index:02d}"

    def describe(self) -> str:
        return (
            f"{self.label}: train {len(self.train)} bars ({self.train.start_iso} → "
            f"{self.train.end_iso}), test {len(self.test)} bars "
            f"({self.test.start_iso} → {self.test.end_iso})"
        )


def build_walk_forward_windows(
    candles: list[Candle],
    *,
    windows: int = 6,
    mode: str = "rolling",
    embargo_bars: int = 20,
    train_ratio: float = 0.7,
) -> list[WalkForwardWindow]:
    """Build walk-forward windows.

    * ``rolling`` — a fixed-length training window slides forward (adapts to
      regime change, forgets old history).
    * ``anchored`` — training always starts at the beginning and grows (uses all
      history, adapts more slowly).

    Test segments never overlap, so each out-of-sample result is an independent
    observation for the consistency score.
    """
    total = len(candles)
    if windows < 2:
        raise ConfigError("walk-forward analysis needs at least 2 windows")
    if total < windows * 60:
        return []

    test_size = total // (windows + 1)
    train_size = int(test_size / (1 - train_ratio) * train_ratio)
    result: list[WalkForwardWindow] = []

    for i in range(windows):
        test_start = total - (windows - i) * test_size
        test_end = test_start + test_size
        if mode == "anchored":
            train_start = 0
        else:
            train_start = max(0, test_start - embargo_bars - train_size)
        train_end = max(train_start, test_start - embargo_bars)

        if train_end - train_start < 60 or test_end - test_start < 20:
            continue

        result.append(
            WalkForwardWindow(
                index=i,
                train=DataSegment(f"wf{i:02d}_train", candles[train_start:train_end]),
                test=DataSegment(f"wf{i:02d}_test", candles[test_start:test_end]),
            )
        )

    return result
