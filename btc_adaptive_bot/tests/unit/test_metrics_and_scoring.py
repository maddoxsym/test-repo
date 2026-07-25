"""Metrics, multi-factor scoring, champion selection, and challenger promotion.

The two headline requirements from the brief are asserted directly:

* a strategy with a 95% win rate and one catastrophic loss must **not** win
* a strategy with +100% return on 4 trades must **not** win
"""

from __future__ import annotations

import math

import pytest

from btcbot.config.schema import ChampionConfig, ScoringConfig
from btcbot.database.repositories import ChampionRepository
from btcbot.learning.metrics import StrategyMetrics, compute_metrics, merge_metrics
from btcbot.scoring.champion import ChampionSelector
from btcbot.scoring.scorer import StrategyEvidence, StrategyScorer, rank_strategies
from btcbot.utils.stats import (
    bootstrap_mean_ci,
    concentration_ratio,
    max_drawdown,
    sample_size_credit,
    welch_t_statistic,
    wilson_interval,
)


def _trade(pnl: float, r: float, **extra) -> dict:
    base = {
        "pnl": pnl,
        "r_multiple": r,
        "regime": "TREND_UP",
        "timeframe": "15",
        "hour_utc": 12,
        "weekday_utc": 2,
        "confidence": 0.6,
        "exit_reason": "take_profit" if pnl > 0 else "stop_loss",
        "direction": "long",
        "duration_seconds": 3600,
        "fees": abs(pnl) * 0.01,
        "slippage_cost": abs(pnl) * 0.005,
        "spread_cost": 0.0,
        "mfe": max(0.0, pnl * 1.3),
        "mae": min(0.0, pnl * 0.4),
        "notional": 1_000.0,
        "volatility_state": "normal",
        "news_state": "calm",
        "exit_ts_utc": "2026-07-24T12:00:00Z",
    }
    base.update(extra)
    return base


class TestMetricsCorrectness:
    def test_hand_checked_metric_values(self):
        """3 wins of +100, 2 losses of -50: every figure computed by hand."""
        trades = [
            _trade(100.0, 2.0), _trade(100.0, 2.0), _trade(100.0, 2.0),
            _trade(-50.0, -1.0), _trade(-50.0, -1.0),
        ]
        metrics = compute_metrics(trades, strategy_id="s", initial_equity=10_000.0)

        assert metrics.total_trades == 5
        assert metrics.wins == 3 and metrics.losses == 2
        assert math.isclose(metrics.win_rate, 0.6)
        assert math.isclose(metrics.gross_profit, 300.0)
        assert math.isclose(metrics.gross_loss, 100.0)
        assert math.isclose(metrics.net_pnl, 200.0)
        assert math.isclose(metrics.profit_factor, 3.0)
        assert math.isclose(metrics.average_win, 100.0)
        assert math.isclose(metrics.average_loss, 50.0)
        assert math.isclose(metrics.win_loss_ratio, 2.0)
        assert math.isclose(metrics.expectancy, 40.0)
        # (3×2 + 2×-1) / 5 = 0.8R
        assert math.isclose(metrics.expectancy_r, 0.8)
        assert math.isclose(metrics.return_pct, 0.02)
        assert math.isclose(metrics.final_equity, 10_200.0)
        assert metrics.longest_winning_streak == 3
        assert metrics.longest_losing_streak == 2

    def test_max_drawdown_is_measured_on_the_equity_path(self):
        trades = [_trade(100.0, 1.0), _trade(-300.0, -3.0), _trade(50.0, 0.5)]
        metrics = compute_metrics(trades, initial_equity=10_000.0)
        # Peak 10,100 → trough 9,800 = 300 absolute.
        assert math.isclose(metrics.max_drawdown, 300.0)
        assert math.isclose(metrics.max_drawdown_pct, 300.0 / 10_100.0)

    def test_profit_factor_when_there_are_no_losses(self):
        metrics = compute_metrics([_trade(100.0, 1.0), _trade(50.0, 0.5)])
        assert metrics.profit_factor > 0
        assert math.isfinite(metrics.profit_factor)

    def test_empty_trade_list_is_safe(self):
        metrics = compute_metrics([], strategy_id="s")
        assert metrics.total_trades == 0
        assert metrics.expectancy_r == 0.0
        assert metrics.profit_factor == 0.0

    def test_all_breakdowns_are_populated(self):
        trades = [
            _trade(100.0, 1.0, regime="TREND_UP", hour_utc=3, weekday_utc=1,
                   volatility_state="high", news_state="calm", timeframe="5"),
            _trade(-50.0, -1.0, regime="RANGING", hour_utc=15, weekday_utc=5,
                   volatility_state="low", news_state="elevated", timeframe="15"),
        ]
        metrics = compute_metrics(trades)
        for breakdown in (
            metrics.by_regime, metrics.by_timeframe, metrics.by_hour, metrics.by_weekday,
            metrics.by_volatility_state, metrics.by_news_state, metrics.by_exit_reason,
            metrics.by_direction,
        ):
            assert breakdown, "a required breakdown is empty"
        assert set(metrics.by_regime) == {"TREND_UP", "RANGING"}

    def test_best_and_worst_regime_need_a_minimum_sample(self):
        trades = [_trade(100.0, 1.0, regime="TREND_UP")]
        metrics = compute_metrics(trades)
        assert metrics.best_regime == "insufficient_data"

        trades = [_trade(100.0, 1.0, regime="TREND_UP") for _ in range(4)]
        trades += [_trade(-100.0, -1.0, regime="RANGING") for _ in range(4)]
        metrics = compute_metrics(trades)
        assert metrics.best_regime == "TREND_UP"
        assert metrics.worst_regime == "RANGING"

    def test_single_winner_concentration_is_detected(self):
        trades = [_trade(1_000.0, 10.0)] + [_trade(10.0, 0.1) for _ in range(9)]
        metrics = compute_metrics(trades)
        assert metrics.single_winner_concentration > 0.9

    def test_fees_and_slippage_are_accumulated(self):
        trades = [_trade(100.0, 1.0) for _ in range(10)]
        metrics = compute_metrics(trades)
        assert math.isclose(metrics.total_fees, 10.0)
        assert math.isclose(metrics.total_slippage, 5.0)

    def test_merge_weights_by_trade_count(self):
        many = compute_metrics([_trade(100.0, 1.0) for _ in range(80)])
        few = compute_metrics([_trade(-100.0, -1.0) for _ in range(5)])
        merged = merge_metrics([many, few], strategy_id="s", layer="walkforward")
        assert merged.total_trades == 85
        # The 80-trade window must dominate.
        assert merged.expectancy_r > 0.7


class TestStatisticalHelpers:
    def test_bootstrap_interval_brackets_the_mean(self):
        values = [1.0, -1.0, 2.0, -0.5, 1.5, 0.5, -1.0, 2.0]
        low, point, high = bootstrap_mean_ci(values, samples=2_000, confidence=0.9)
        assert low <= point <= high
        assert math.isclose(point, sum(values) / len(values))

    def test_bootstrap_is_reproducible(self):
        values = [1.0, -1.0, 2.0, -0.5]
        assert bootstrap_mean_ci(values) == bootstrap_mean_ci(values)

    def test_wilson_interval_handles_small_samples(self):
        low, high = wilson_interval(1, 1)
        assert 0.0 <= low <= 1.0 <= high + 1e-9
        assert low < 1.0, "a 1/1 sample must not imply certainty"

    def test_sample_size_credit_gives_zero_below_the_floor(self):
        assert sample_size_credit(3, full=40, minimum=8) == 0.0
        assert sample_size_credit(8, full=40, minimum=8) == 0.0
        assert 0.0 < sample_size_credit(20, full=40, minimum=8) < 1.0
        assert sample_size_credit(40, full=40, minimum=8) == 1.0
        assert sample_size_credit(500, full=40, minimum=8) == 1.0

    def test_max_drawdown_helper(self):
        absolute, fractional = max_drawdown([100.0, 120.0, 90.0, 110.0])
        assert math.isclose(absolute, 30.0)
        assert math.isclose(fractional, 0.25)

    def test_concentration_ratio(self):
        assert math.isclose(concentration_ratio([100.0, 10.0, 10.0]), 100.0 / 120.0)
        assert concentration_ratio([-1.0, -2.0]) == 0.0

    def test_welch_t_detects_a_real_difference(self):
        low = [0.1] * 30
        high = [1.0] * 30
        assert welch_t_statistic(high, low) > 1.96

    def test_welch_t_is_small_for_similar_samples(self):
        a = [0.5, -0.5, 0.6, -0.4, 0.5, -0.5]
        b = [0.45, -0.45, 0.55, -0.35, 0.5, -0.5]
        assert abs(welch_t_statistic(a, b)) < 1.96


class TestScoringGuards:
    """The two failure modes the brief explicitly forbids."""

    @pytest.fixture
    def scorer(self) -> StrategyScorer:
        return StrategyScorer(ScoringConfig())

    def test_high_win_rate_with_one_catastrophic_loss_is_penalised(self, scorer):
        """19 small wins and one loss that wipes them out: 95% win rate."""
        trades = [_trade(10.0, 0.2) for _ in range(19)] + [_trade(-500.0, -10.0)]
        metrics = compute_metrics(trades, strategy_id="lucky", initial_equity=10_000.0)
        assert math.isclose(metrics.win_rate, 0.95)
        assert metrics.net_pnl < 0

        solid = compute_metrics(
            [_trade(60.0, 1.2) for _ in range(24)] + [_trade(-50.0, -1.0) for _ in range(16)],
            strategy_id="solid",
            initial_equity=10_000.0,
        )
        assert solid.win_rate < 0.7, "the comparison strategy should have a lower win rate"

        lucky_score = scorer.score(
            StrategyEvidence(strategy_id="lucky", shadow=metrics, historical=metrics)
        )
        solid_score = scorer.score(
            StrategyEvidence(strategy_id="solid", shadow=solid, historical=solid)
        )
        assert solid_score.final_score > lucky_score.final_score, (
            "a 95% win rate with a catastrophic loss beat a consistent strategy"
        )
        assert lucky_score.penalties, "the catastrophic loss earned no penalty"

    def test_huge_return_on_four_trades_is_not_credited(self, scorer):
        """+100% on 4 trades must score below a modest, well-evidenced record."""
        spectacular = compute_metrics(
            [_trade(2_500.0, 5.0) for _ in range(4)], strategy_id="tiny", initial_equity=10_000.0
        )
        assert math.isclose(spectacular.return_pct, 1.0)

        modest = compute_metrics(
            [_trade(40.0, 0.8) for _ in range(30)] + [_trade(-45.0, -0.9) for _ in range(20)],
            strategy_id="steady",
            initial_equity=10_000.0,
        )
        assert modest.return_pct < 0.05

        tiny_score = scorer.score(
            StrategyEvidence(strategy_id="tiny", shadow=spectacular, historical=spectacular)
        )
        steady_score = scorer.score(
            StrategyEvidence(strategy_id="steady", shadow=modest, historical=modest)
        )
        assert steady_score.final_score > tiny_score.final_score, (
            "a 4-trade +100% record outranked a 50-trade positive record"
        )
        assert "tiny_sample" in tiny_score.penalties or "small_sample" in tiny_score.penalties

    def test_below_minimum_sample_earns_zero_expectancy_credit(self, scorer):
        metrics = compute_metrics([_trade(500.0, 5.0) for _ in range(3)], initial_equity=10_000.0)
        breakdown = scorer.score(StrategyEvidence(strategy_id="s", historical=metrics))
        assert breakdown.components["oos_expectancy"] == 0.0

    def test_no_evidence_scores_zero(self, scorer):
        breakdown = scorer.score(StrategyEvidence(strategy_id="empty"))
        assert breakdown.final_score == 0.0
        assert breakdown.confidence == "LOW"

    def test_negative_out_of_sample_is_penalised(self, scorer):
        losing = compute_metrics(
            [_trade(-50.0, -1.0) for _ in range(20)], initial_equity=10_000.0
        )
        breakdown = scorer.score(StrategyEvidence(strategy_id="loser", historical=losing))
        assert "oos_negative" in breakdown.penalties

    def test_parameter_instability_is_penalised(self, scorer):
        metrics = compute_metrics(
            [_trade(50.0, 1.0) for _ in range(30)] + [_trade(-40.0, -0.8) for _ in range(20)],
            initial_equity=10_000.0,
        )
        stable = scorer.score(
            StrategyEvidence(
                strategy_id="stable", shadow=metrics, historical=metrics, parameter_stability=0.9
            )
        )
        spiky = scorer.score(
            StrategyEvidence(
                strategy_id="spiky", shadow=metrics, historical=metrics, parameter_stability=0.1
            )
        )
        assert stable.final_score > spiky.final_score
        assert "parameter_spike" in spiky.penalties

    def test_score_is_bounded(self, scorer):
        excellent = compute_metrics(
            [_trade(200.0, 2.0) for _ in range(200)], initial_equity=10_000.0
        )
        breakdown = scorer.score(
            StrategyEvidence(
                strategy_id="great", shadow=excellent, historical=excellent, demo=excellent,
                regime_stability=1.0, parameter_stability=1.0,
            )
        )
        assert 0.0 <= breakdown.final_score <= 100.0

    def test_confidence_requires_multi_layer_evidence(self, scorer):
        good = compute_metrics(
            [_trade(60.0, 1.2) for _ in range(40)] + [_trade(-50.0, -1.0) for _ in range(20)],
            initial_equity=10_000.0,
        )
        one_layer = scorer.score(StrategyEvidence(strategy_id="one", shadow=good))
        assert one_layer.confidence in {"LOW", "MEDIUM"}

    def test_breakdown_explains_itself(self, scorer):
        metrics = compute_metrics([_trade(50.0, 1.0) for _ in range(30)], initial_equity=10_000.0)
        breakdown = scorer.score(StrategyEvidence(strategy_id="s", shadow=metrics))
        assert breakdown.explain()
        assert len(breakdown.components) == 12, "all twelve components must be recorded"

    def test_ranking_prefers_better_evidence_on_ties(self):
        from btcbot.scoring.scorer import ScoreBreakdown

        thin = ScoreBreakdown(strategy_id="thin", final_score=50.0, total_observations=10)
        thick = ScoreBreakdown(strategy_id="thick", final_score=50.0, total_observations=100)
        ranked = rank_strategies([thin, thick])
        assert ranked[0].strategy_id == "thick"


class TestChampionSelection:
    @pytest.fixture
    def selector(self, temp_db) -> ChampionSelector:
        return ChampionSelector(ChampionConfig(), ChampionRepository(temp_db))

    def _evidence(self, strategy_id: str, *, trades: int, r: float) -> StrategyEvidence:
        rows = [_trade(50.0 * r, r) for _ in range(trades)]
        metrics = compute_metrics(rows, strategy_id=strategy_id, initial_equity=10_000.0)
        return StrategyEvidence(
            strategy_id=strategy_id, shadow=metrics, historical=metrics, demo=metrics
        )

    def test_no_champion_when_evidence_is_insufficient(self, selector):
        from btcbot.scoring.scorer import ScoreBreakdown

        breakdown = ScoreBreakdown(strategy_id="thin", final_score=20.0, total_observations=5)
        selection = selector.select(
            [breakdown],
            {"thin": self._evidence("thin", trades=2, r=1.0)},
            experiment_id="exp_1",
        )
        assert selection.champion_type == "none"
        assert selection.champion_id is None
        assert "observations" in selection.why_it_won or "layers" in selection.why_it_won

    def test_no_champion_when_nothing_scored(self, selector):
        selection = selector.select([], {}, experiment_id="exp_1")
        assert selection.champion_type == "none"

    def test_single_champion_selected_with_strong_evidence(self, selector, scoring_config):
        scorer = StrategyScorer(scoring_config)
        evidence_map = {
            "winner": self._evidence("winner", trades=60, r=1.0),
            "loser": self._evidence("loser", trades=60, r=-0.4),
        }
        ranked = rank_strategies([scorer.score(e) for e in evidence_map.values()])
        selection = selector.select(ranked, evidence_map, experiment_id="exp_1")

        assert selection.champion_type == "single"
        assert selection.champion_id == "winner"
        assert selection.why_it_won
        assert selection.challengers

    def test_ensemble_wins_when_regimes_split(self, selector, scoring_config):
        scorer = StrategyScorer(scoring_config)
        evidence_map = {
            "trend_specialist": self._evidence("trend_specialist", trades=50, r=0.5),
            "range_specialist": self._evidence("range_specialist", trades=50, r=0.5),
        }
        ranked = rank_strategies([scorer.score(e) for e in evidence_map.values()])
        # Each strategy owns a different regime, decisively.
        regime_performance = {
            "trend_specialist": {"TREND_UP": 1.4, "RANGING": -0.5},
            "range_specialist": {"TREND_UP": -0.5, "RANGING": 1.4},
        }
        selection = selector.select(
            ranked, evidence_map, experiment_id="exp_1", regime_performance=regime_performance
        )
        assert selection.champion_type == "ensemble"
        assert len(selection.ensemble_members) >= 2
        assert len(set(selection.ensemble_members.values())) >= 2
        assert "regime" in selection.why_it_won.lower()

    def test_ensemble_rejected_when_one_strategy_owns_everything(self, selector, scoring_config):
        scorer = StrategyScorer(scoring_config)
        evidence_map = {
            "dominant": self._evidence("dominant", trades=60, r=1.0),
            "weak": self._evidence("weak", trades=60, r=0.1),
        }
        ranked = rank_strategies([scorer.score(e) for e in evidence_map.values()])
        regime_performance = {
            "dominant": {"TREND_UP": 1.2, "RANGING": 1.1},
            "weak": {"TREND_UP": 0.1, "RANGING": 0.05},
        }
        selection = selector.select(
            ranked, evidence_map, experiment_id="exp_1", regime_performance=regime_performance
        )
        assert selection.champion_type == "single"
        assert selection.champion_id == "dominant"

    def test_selection_is_recorded_in_history(self, selector, scoring_config, temp_db):
        scorer = StrategyScorer(scoring_config)
        evidence_map = {"winner": self._evidence("winner", trades=60, r=1.0)}
        ranked = rank_strategies([scorer.score(e) for e in evidence_map.values()])
        selector.select(ranked, evidence_map, experiment_id="exp_1")

        current = ChampionRepository(temp_db).current("exp_1")
        assert current is not None
        assert current["new_champion"] == "winner"
        assert current["reason"]

    def test_banner_contains_the_required_fields(self, selector, scoring_config):
        scorer = StrategyScorer(scoring_config)
        evidence_map = {
            "winner": self._evidence("winner", trades=60, r=1.0),
            "second": self._evidence("second", trades=50, r=0.4),
        }
        ranked = rank_strategies([scorer.score(e) for e in evidence_map.values()])
        selection = selector.select(ranked, evidence_map, experiment_id="exp_1")
        text = "\n".join(selection.banner())
        for expected in ("CHAMPION", "WHY IT WON", "CONFIDENCE", "CHALLENGERS"):
            assert expected in text


class TestChallengerPromotion:
    @pytest.fixture
    def selector(self, temp_db) -> ChampionSelector:
        return ChampionSelector(ChampionConfig(), ChampionRepository(temp_db))

    def test_three_lucky_wins_do_not_promote(self, selector):
        """The exact failure the brief calls out."""
        champion_r = [0.5] * 60
        challenger_r = [3.0, 3.0, 3.0]
        promoted, reason = selector.evaluate_promotion(
            champion_id="champ",
            champion_metrics=compute_metrics([_trade(25.0, 0.5) for _ in range(60)]),
            challenger_id="hot_streak",
            challenger_metrics=compute_metrics([_trade(150.0, 3.0) for _ in range(3)]),
            champion_r=champion_r,
            challenger_r=challenger_r,
            experiment_id="exp_1",
        )
        assert not promoted
        assert "new trades" in reason

    def test_no_promotion_without_expectancy_improvement(self, selector):
        trades = [_trade(25.0, 0.5) for _ in range(40)]
        promoted, reason = selector.evaluate_promotion(
            champion_id="champ",
            champion_metrics=compute_metrics(trades),
            challenger_id="same",
            challenger_metrics=compute_metrics(trades),
            champion_r=[0.5] * 40,
            challenger_r=[0.5] * 40,
            experiment_id="exp_1",
        )
        assert not promoted
        assert "expectancy improvement" in reason

    def test_no_promotion_without_statistical_significance(self, selector):
        import numpy as np

        rng = np.random.default_rng(3)
        champion_r = rng.normal(0.3, 3.0, 40).tolist()
        challenger_r = rng.normal(0.45, 3.0, 40).tolist()
        promoted, reason = selector.evaluate_promotion(
            champion_id="champ",
            champion_metrics=compute_metrics([_trade(r * 50, r) for r in champion_r]),
            challenger_id="noisy",
            challenger_metrics=compute_metrics([_trade(r * 50, r) for r in challenger_r]),
            champion_r=champion_r,
            challenger_r=challenger_r,
            experiment_id="exp_1",
        )
        assert not promoted

    def test_promotion_succeeds_on_strong_evidence(self, selector):
        champion_r = [0.1] * 60
        challenger_r = [1.2] * 60
        champion_trades = [_trade(5.0, 0.1) for _ in range(60)]
        challenger_trades = [
            _trade(60.0, 1.2, regime="TREND_UP" if i % 2 else "RANGING") for i in range(60)
        ]
        promoted, reason = selector.evaluate_promotion(
            champion_id="champ",
            champion_metrics=compute_metrics(champion_trades),
            challenger_id="better",
            challenger_metrics=compute_metrics(challenger_trades),
            champion_r=champion_r,
            challenger_r=challenger_r,
            experiment_id="exp_1",
        )
        assert promoted, reason
        assert "expectancy improved" in reason

    def test_promotion_requires_multiple_positive_regimes(self, selector):
        challenger_trades = [_trade(60.0, 1.2, regime="TREND_UP") for _ in range(60)]
        promoted, reason = selector.evaluate_promotion(
            champion_id="champ",
            champion_metrics=compute_metrics([_trade(5.0, 0.1) for _ in range(60)]),
            challenger_id="one_regime",
            challenger_metrics=compute_metrics(challenger_trades),
            champion_r=[0.1] * 60,
            challenger_r=[1.2] * 60,
            experiment_id="exp_1",
        )
        assert not promoted
        assert "fewer than two regimes" in reason

    def test_promotion_is_recorded(self, selector, temp_db):
        challenger_trades = [
            _trade(60.0, 1.2, regime="TREND_UP" if i % 2 else "RANGING") for i in range(60)
        ]
        selector.evaluate_promotion(
            champion_id="champ",
            champion_metrics=compute_metrics([_trade(5.0, 0.1) for _ in range(60)]),
            challenger_id="better",
            challenger_metrics=compute_metrics(challenger_trades),
            champion_r=[0.1] * 60,
            challenger_r=[1.2] * 60,
            experiment_id="exp_1",
        )
        history = ChampionRepository(temp_db).history()
        assert history
        assert history[0]["previous_champion"] == "champ"
        assert history[0]["new_champion"] == "better"


class TestDecayDetection:
    @pytest.fixture
    def selector(self, temp_db) -> ChampionSelector:
        return ChampionSelector(ChampionConfig(), ChampionRepository(temp_db))

    def test_normal_losing_run_is_not_decay(self, selector):
        import numpy as np

        rng = np.random.default_rng(11)
        r_values = rng.normal(0.4, 1.0, 100).tolist()
        decayed, reason = selector.detect_decay(
            "champ", r_values, compute_metrics([_trade(r * 50, r) for r in r_values])
        )
        assert not decayed, reason

    def test_genuine_deterioration_is_detected(self, selector):
        r_values = [1.2] * 60 + [-0.9] * 30
        decayed, reason = selector.detect_decay(
            "champ", r_values, compute_metrics([_trade(r * 50, r) for r in r_values])
        )
        assert decayed
        assert "expectancy fell" in reason

    def test_insufficient_history_does_not_flag(self, selector):
        decayed, reason = selector.detect_decay("champ", [0.5] * 10, StrategyMetrics())
        assert not decayed
        assert "insufficient" in reason
