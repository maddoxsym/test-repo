"""Statistics used for evidence-weighted decisions.

Plain, auditable statistics — bootstrap confidence intervals, Student-t
posteriors, Wilson intervals. No black-box models: every number the champion
selection depends on can be recomputed by hand from the stored trade list.
"""

from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np

from .numeric import safe_div

__all__ = [
    "mean",
    "stdev",
    "bootstrap_mean_ci",
    "wilson_interval",
    "student_t_sample",
    "sharpe_ratio",
    "sortino_ratio",
    "max_drawdown",
    "concentration_ratio",
    "herfindahl",
    "sample_size_credit",
    "welch_t_statistic",
]


def mean(values: Sequence[float]) -> float:
    return float(np.mean(values)) if len(values) else 0.0


def stdev(values: Sequence[float], *, ddof: int = 1) -> float:
    """Sample standard deviation; 0.0 when there is not enough data."""
    if len(values) <= ddof:
        return 0.0
    result = float(np.std(np.asarray(values, dtype=float), ddof=ddof))
    return result if math.isfinite(result) else 0.0


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    samples: int = 2000,
    confidence: float = 0.9,
    seed: int = 12345,
) -> tuple[float, float, float]:
    """Bootstrap ``(low, point, high)`` for the mean of ``values``.

    Used to penalise uncertainty in scoring: a strategy whose expectancy
    confidence interval straddles zero should not outrank one whose interval sits
    comfortably above it, even at equal point estimates.

    The seed is fixed so that a given dataset always scores identically — the
    ranking must be reproducible and auditable.
    """
    n = len(values)
    if n == 0:
        return (0.0, 0.0, 0.0)
    arr = np.asarray(values, dtype=float)
    point = float(arr.mean())
    if n == 1:
        return (point, point, point)

    rng = np.random.default_rng(seed)
    draws = rng.integers(0, n, size=(samples, n))
    means = arr[draws].mean(axis=1)
    tail = (1.0 - confidence) / 2.0
    low = float(np.quantile(means, tail))
    high = float(np.quantile(means, 1.0 - tail))
    return (low, point, high)


def wilson_interval(successes: int, trials: int, *, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a win rate.

    Preferred over the naive normal interval because it stays sensible with the
    small samples this system routinely has to reason about.
    """
    if trials <= 0:
        return (0.0, 1.0)
    p = successes / trials
    denom = 1.0 + z * z / trials
    centre = (p + z * z / (2 * trials)) / denom
    margin = z * math.sqrt(safe_div(p * (1 - p), trials) + z * z / (4 * trials * trials)) / denom
    return (max(0.0, centre - margin), min(1.0, centre + margin))


def student_t_sample(
    values: Sequence[float],
    rng: np.random.Generator,
    *,
    prior_mean: float = 0.0,
    prior_strength: float = 1.0,
) -> float:
    """Draw one sample of the plausible mean of ``values`` (Thompson sampling).

    Models the unknown mean with a Normal-inverse-chi-squared posterior and
    samples from its Student-t marginal. A weak prior centred on ``prior_mean``
    carries Layer-1/Layer-2 evidence into the allocator so a strategy with no
    live demo trades is not treated as a blank slate.
    """
    n = len(values)
    if n == 0:
        # No observations: the prior is all we have, widened so exploration wins.
        return float(rng.normal(prior_mean, 1.0))

    arr = np.asarray(values, dtype=float)
    kappa0 = max(1e-6, prior_strength)
    kappa_n = kappa0 + n
    mu_n = (kappa0 * prior_mean + arr.sum()) / kappa_n
    nu_n = kappa0 + n

    sample_var = float(arr.var(ddof=1)) if n > 1 else 1.0
    if not math.isfinite(sample_var) or sample_var <= 0:
        sample_var = 1e-6
    scale = math.sqrt(max(1e-12, sample_var / kappa_n))
    return float(mu_n + scale * rng.standard_t(df=max(1.0, nu_n)))


def sharpe_ratio(returns: Sequence[float], *, periods_per_year: float = 365.0) -> float:
    """Annualised Sharpe of a per-trade or per-period return series."""
    if len(returns) < 2:
        return 0.0
    sd = stdev(returns)
    if sd <= 0:
        return 0.0
    return safe_div(mean(returns), sd) * math.sqrt(periods_per_year)


def sortino_ratio(returns: Sequence[float], *, periods_per_year: float = 365.0) -> float:
    """Annualised Sortino — like Sharpe but only downside deviation is penalised."""
    if len(returns) < 2:
        return 0.0
    arr = np.asarray(returns, dtype=float)
    downside = arr[arr < 0]
    if downside.size == 0:
        # No losing periods at all: real, but not evidence of infinite quality.
        return 0.0
    downside_dev = float(np.sqrt(np.mean(np.square(downside))))
    if downside_dev <= 0:
        return 0.0
    return safe_div(float(arr.mean()), downside_dev) * math.sqrt(periods_per_year)


def max_drawdown(equity_curve: Sequence[float]) -> tuple[float, float]:
    """Return ``(absolute_drawdown, fractional_drawdown)`` of an equity curve."""
    if len(equity_curve) < 2:
        return (0.0, 0.0)
    arr = np.asarray(equity_curve, dtype=float)
    running_peak = np.maximum.accumulate(arr)
    drawdowns = running_peak - arr
    idx = int(np.argmax(drawdowns))
    abs_dd = float(drawdowns[idx])
    peak = float(running_peak[idx])
    return (abs_dd, safe_div(abs_dd, peak))


def concentration_ratio(profits: Sequence[float]) -> float:
    """Share of gross profit contributed by the single best trade.

    A high value means the record depends on one lucky outlier — exactly the kind
    of result the anti-overfitting system is required to punish.
    """
    wins = [p for p in profits if p > 0]
    if not wins:
        return 0.0
    return safe_div(max(wins), sum(wins))


def herfindahl(values: Sequence[float]) -> float:
    """Herfindahl index of positive contributions; 1.0 = everything from one source."""
    positives = [v for v in values if v > 0]
    total = sum(positives)
    if total <= 0:
        return 0.0
    return sum((v / total) ** 2 for v in positives)


def sample_size_credit(n: int, *, full: int, minimum: int) -> float:
    """Scale evidence from 0 → 1 as the sample grows from ``minimum`` to ``full``.

    Below ``minimum`` the strategy earns no credit at all; that is what stops a
    4-trade, +100% record from winning the experiment.
    """
    if n < minimum:
        return 0.0
    if n >= full:
        return 1.0
    span = max(1, full - minimum)
    return math.sqrt((n - minimum) / span)


def welch_t_statistic(a: Sequence[float], b: Sequence[float]) -> float:
    """Welch's t comparing two independent means (unequal variances).

    Used by challenger promotion: a challenger must beat the champion by a
    statistically meaningful margin, not merely a nominal one.
    """
    if len(a) < 2 or len(b) < 2:
        return 0.0
    va, vb = stdev(a) ** 2, stdev(b) ** 2
    denom = math.sqrt(safe_div(va, len(a)) + safe_div(vb, len(b)))
    if denom <= 0:
        return 0.0
    return (mean(a) - mean(b)) / denom
