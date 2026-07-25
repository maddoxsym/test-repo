"""Configuration validation, shadow-account isolation, and the news engine."""

from __future__ import annotations

import math
from datetime import timedelta
from pathlib import Path

import pytest

from btcbot.config.loader import load_config, load_credentials
from btcbot.config.schema import (
    AppConfig,
    BacktestingConfig,
    MarketConfig,
    NewsConfig,
    RegimeConfig,
    RiskConfig,
    ScoringConfig,
    ShadowConfig,
)
from btcbot.utils.errors import ConfigError, CredentialsMissingError
from btcbot.utils.timeutil import iso, now_utc


class TestConfigValidation:
    def test_defaults_are_valid(self):
        config = AppConfig()
        assert config.experiment.research_duration_days == 14
        assert config.shadow.initial_equity == 10_000.0
        assert config.experiment.expected_demo_equity == 10_000.0
        assert config.market.primary_symbol == "BTCUSDT"

    def test_unknown_key_is_rejected(self):
        """A typo must fail loudly, not be silently ignored."""
        with pytest.raises(Exception):
            RiskConfig(maximum_risk=0.02)  # type: ignore[call-arg]

    def test_risk_ordering_is_enforced(self):
        with pytest.raises(Exception) as exc:
            RiskConfig(min_risk_pct=0.02, normal_risk_pct=0.01, max_risk_pct=0.015)
        assert "min_risk_pct <= normal_risk_pct <= max_risk_pct" in str(exc.value)

    def test_risk_cannot_exceed_five_percent(self):
        with pytest.raises(Exception):
            RiskConfig(max_risk_pct=0.5)

    def test_stop_distance_bounds_must_be_ordered(self):
        with pytest.raises(Exception) as exc:
            RiskConfig(min_stop_distance_pct=0.2, max_stop_distance_pct=0.1)
        assert "min_stop_distance_pct" in str(exc.value)

    def test_invalid_timeframe_is_rejected(self):
        with pytest.raises(Exception) as exc:
            MarketConfig(timeframes=["1", "7"])
        assert "not a Bybit kline interval" in str(exc.value)

    def test_lowercase_symbol_is_rejected(self):
        """Bybit documents symbols as uppercase only."""
        with pytest.raises(Exception) as exc:
            MarketConfig(primary_symbol="btcusdt")
        assert "uppercase" in str(exc.value)

    def test_preferred_category_must_be_enabled(self):
        with pytest.raises(Exception) as exc:
            MarketConfig(enabled_categories=["spot"], preferred_category="linear")
        assert "not in enabled_categories" in str(exc.value)

    def test_regime_timeframe_must_be_collected(self):
        with pytest.raises(Exception) as exc:
            MarketConfig(timeframes=["5", "15"], regime_timeframe="60",
                         regime_context_timeframe="15")
        assert "regime_timeframe" in str(exc.value)

    def test_splits_must_leave_out_of_sample_data(self):
        with pytest.raises(Exception) as exc:
            BacktestingConfig(train_fraction=0.8, validation_fraction=0.3)
        assert "out-of-sample" in str(exc.value)

    def test_adx_thresholds_must_be_ordered(self):
        with pytest.raises(Exception) as exc:
            RegimeConfig(adx_trend_threshold=40.0, adx_strong_trend_threshold=30.0)
        assert "adx_strong_trend_threshold" in str(exc.value)

    def test_range_percentiles_must_be_ordered(self):
        with pytest.raises(Exception):
            RegimeConfig(range_compression_percentile=80.0, range_expansion_percentile=20.0)

    def test_scoring_sample_thresholds_must_be_ordered(self):
        with pytest.raises(Exception) as exc:
            ScoringConfig(min_trades_any_credit=50, min_trades_full_credit=40)
        assert "min_trades_any_credit" in str(exc.value)

    def test_scoring_weights_cannot_all_be_zero(self):
        from btcbot.config.schema import ScoringWeights

        with pytest.raises(Exception):
            ScoringWeights(
                oos_expectancy=0.0, walk_forward_consistency=0.0, shadow_expectancy=0.0,
                demo_performance=0.0, profit_factor=0.0, risk_adjusted_return=0.0,
                max_drawdown=0.0, observations=0.0, regime_stability=0.0,
                parameter_stability=0.0, cost_robustness=0.0, consistency=0.0,
            )

    def test_news_influence_band_must_be_ordered(self):
        with pytest.raises(Exception) as exc:
            NewsConfig(influence_floor=0.8, influence_ceiling=0.2)
        assert "influence_floor" in str(exc.value)

    def test_strategy_cannot_be_both_enabled_and_disabled(self):
        with pytest.raises(Exception) as exc:
            AppConfig(strategies={"enabled": ["a"], "disabled": ["a"]})
        assert "both enabled and disabled" in str(exc.value)

    def test_history_timeframes_must_be_collected(self):
        with pytest.raises(Exception) as exc:
            AppConfig(
                market={"timeframes": ["5", "15", "60", "240"], "regime_timeframe": "60",
                        "regime_context_timeframe": "240"},
                data={"history_timeframes": ["5", "30"]},
            )
        assert "history_timeframes" in str(exc.value)

    def test_config_is_frozen(self):
        config = RiskConfig()
        with pytest.raises(Exception):
            config.max_risk_pct = 0.5  # type: ignore[misc]


class TestConfigLoading:
    def test_shipped_config_files_load(self):
        root = Path(__file__).resolve().parents[2]
        for name in ("default.yaml", "research.yaml", "champion.yaml"):
            loaded = load_config(root / "config" / name)
            assert loaded.config.experiment.research_duration_days == 14
            assert len(loaded.config_hash) == 16

    def test_extends_chain_is_merged(self):
        root = Path(__file__).resolve().parents[2]
        research = load_config(root / "config" / "research.yaml")
        # Value inherited from default.yaml.
        assert research.config.market.primary_symbol == "BTCUSDT"
        # Value overridden by research.yaml.
        assert research.config.allocator.forced_exploration_ratio == 0.3

    def test_config_hash_reflects_content(self, tmp_path):
        first = tmp_path / "a.yaml"
        first.write_text("experiment:\n  research_duration_days: 14\n")
        second = tmp_path / "b.yaml"
        second.write_text("experiment:\n  research_duration_days: 7\n")
        assert load_config(first).config_hash != load_config(second).config_hash

    def test_missing_file_raises_clearly(self, tmp_path):
        with pytest.raises(ConfigError) as exc:
            load_config(tmp_path / "nope.yaml")
        assert "not found" in str(exc.value)

    def test_invalid_yaml_raises_clearly(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("experiment:\n  - this: [is\n   broken")
        with pytest.raises(ConfigError) as exc:
            load_config(path)
        assert "not valid YAML" in str(exc.value)

    def test_validation_error_names_the_field(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text("risk:\n  max_risk_pct: 99.0\n")
        with pytest.raises(ConfigError) as exc:
            load_config(path)
        assert "risk.max_risk_pct" in str(exc.value)

    def test_extends_cycle_is_detected(self, tmp_path):
        a = tmp_path / "a.yaml"
        b = tmp_path / "b.yaml"
        a.write_text("extends: b.yaml\n")
        b.write_text("extends: a.yaml\n")
        with pytest.raises(ConfigError) as exc:
            load_config(a)
        assert "cycle" in str(exc.value)

    def test_missing_credentials_gives_actionable_guidance(self, monkeypatch):
        monkeypatch.delenv("BYBIT_DEMO_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_DEMO_API_SECRET", raising=False)
        with pytest.raises(CredentialsMissingError) as exc:
            load_credentials(env_file=None, required=True)
        message = str(exc.value)
        assert ".env" in message and "Demo Trading" in message

    def test_credentials_are_optional_when_not_required(self, monkeypatch):
        monkeypatch.delenv("BYBIT_DEMO_API_KEY", raising=False)
        monkeypatch.delenv("BYBIT_DEMO_API_SECRET", raising=False)
        assert load_credentials(env_file=None, required=False) is None

    def test_credentials_repr_does_not_leak(self, monkeypatch):
        monkeypatch.setenv("BYBIT_DEMO_API_KEY", "key1234567890")
        monkeypatch.setenv("BYBIT_DEMO_API_SECRET", "secret1234567890")
        credentials = load_credentials(env_file=None, required=True)
        assert credentials is not None
        assert "key1234567890" not in repr(credentials)
        assert "secret" not in repr(credentials).replace("api_secret", "")


class TestShadowAccountIsolation:
    """Strategy A's balance must never affect Strategy B's."""

    def test_accounts_start_at_exactly_ten_thousand(self):
        from btcbot.shadow.account import ShadowAccount

        for strategy_id in ("a", "b", "c"):
            account = ShadowAccount.create(strategy_id, "exp_1", initial_equity=10_000.0)
            assert account.equity == 10_000.0
            assert account.available == 10_000.0
            assert account.peak_equity == 10_000.0

    def test_one_account_losing_does_not_touch_another(self):
        from btcbot.shadow.account import ShadowAccount

        a = ShadowAccount.create("a", "exp_1")
        b = ShadowAccount.create("b", "exp_1")

        a.book_trade(-5_000.0, fees=10.0, slippage=5.0)
        assert a.equity == 5_000.0
        assert b.equity == 10_000.0, "Strategy A's loss leaked into Strategy B"
        assert b.available == 10_000.0
        assert b.max_drawdown == 0.0

    def test_risk_budget_shrinks_with_equity(self):
        from btcbot.shadow.account import ShadowAccount

        account = ShadowAccount.create("a", "exp_1")
        assert math.isclose(account.risk_budget(0.01), 100.0)
        account.book_trade(-5_000.0, fees=0.0, slippage=0.0)
        assert math.isclose(account.risk_budget(0.01), 50.0)

    def test_drawdown_tracks_the_peak(self):
        from btcbot.shadow.account import ShadowAccount

        account = ShadowAccount.create("a", "exp_1")
        account.book_trade(2_000.0, fees=0.0, slippage=0.0)
        assert account.peak_equity == 12_000.0
        account.book_trade(-3_000.0, fees=0.0, slippage=0.0)
        assert math.isclose(account.max_drawdown, 3_000.0)
        assert math.isclose(account.drawdown_pct, 0.25)

    def test_consecutive_losses_are_tracked(self):
        from btcbot.shadow.account import ShadowAccount

        account = ShadowAccount.create("a", "exp_1")
        for _ in range(3):
            account.book_trade(-100.0, fees=0.0, slippage=0.0)
        assert account.consecutive_losses == 3
        account.book_trade(50.0, fees=0.0, slippage=0.0)
        assert account.consecutive_losses == 0

    def test_serialisation_round_trip_preserves_state(self):
        from btcbot.shadow.account import ShadowAccount

        account = ShadowAccount.create("a", "exp_1")
        account.book_trade(250.0, fees=1.0, slippage=0.5)
        row = account.to_row()
        assert row["strategy_id"] == "a"
        assert math.isclose(row["equity"], 10_250.0)
        assert row["open_position"] is None

    def test_shadow_engine_creates_isolated_accounts(self, repos):
        from btcbot.shadow.engine import ShadowEngine

        engine = ShadowEngine(
            ShadowConfig(), repos.shadow, experiment_id="exp_1", symbol="BTCUSDT"
        )
        engine.initialise(["s1", "s2", "s3"])
        assert len(engine.accounts) == 3
        for account in engine.accounts.values():
            assert account.equity == 10_000.0

        engine.accounts["s1"].book_trade(-2_000.0, 0.0, 0.0)
        assert engine.accounts["s2"].equity == 10_000.0
        assert engine.accounts["s3"].equity == 10_000.0

    def test_accounts_persist_and_restore(self, repos):
        from btcbot.shadow.engine import ShadowEngine

        first = ShadowEngine(ShadowConfig(), repos.shadow, experiment_id="exp_1", symbol="BTCUSDT")
        first.initialise(["s1", "s2"])
        first.accounts["s1"].book_trade(500.0, 1.0, 0.5)
        first.persist()

        second = ShadowEngine(ShadowConfig(), repos.shadow, experiment_id="exp_1", symbol="BTCUSDT")
        second.initialise(["s1", "s2"])
        assert math.isclose(second.accounts["s1"].equity, 10_500.0), "equity was reset on restart"
        assert math.isclose(second.accounts["s2"].equity, 10_000.0)

    def test_snapshot_counts_winners_and_losers(self, repos):
        from btcbot.shadow.engine import ShadowEngine

        engine = ShadowEngine(ShadowConfig(), repos.shadow, experiment_id="exp_1", symbol="BTCUSDT")
        engine.initialise(["w", "l", "flat"])
        engine.accounts["w"].book_trade(100.0, 0.0, 0.0)
        engine.accounts["l"].book_trade(-100.0, 0.0, 0.0)
        snapshot = engine.snapshot()
        assert snapshot["winners"] == 1
        assert snapshot["losers"] == 1
        assert snapshot["accounts"] == 3


class TestNewsScoring:
    def test_bitcoin_headline_is_highly_relevant(self):
        from btcbot.news.scoring import score_relevance

        assert score_relevance("Bitcoin surges past $120,000") == 1.0

    def test_macro_headline_is_relevant(self):
        from btcbot.news.scoring import score_relevance

        assert score_relevance("Federal Reserve holds interest rates steady") >= 0.5

    def test_irrelevant_headline_scores_zero(self):
        from btcbot.news.scoring import score_relevance

        assert score_relevance("Local bakery wins regional pastry award") == 0.0

    def test_categories_are_classified(self):
        from btcbot.news.base import NewsCategory
        from btcbot.news.scoring import classify_category

        assert classify_category("SEC approves spot Bitcoin ETF") in {
            NewsCategory.ETF_INSTITUTIONAL, NewsCategory.CRYPTO_REGULATION
        }
        assert classify_category("Exchange hacked, $200m stolen") is NewsCategory.SECURITY_HACK
        assert classify_category("US CPI inflation comes in hot") is NewsCategory.INFLATION_CPI

    def test_sentiment_direction(self):
        from btcbot.news.scoring import score_sentiment

        assert score_sentiment("Bitcoin rallies to record high on ETF inflows") > 0
        assert score_sentiment("Bitcoin plunges as exchange collapses in fraud probe") < 0
        assert score_sentiment("Bitcoin trades sideways") == 0.0

    def test_negation_is_handled(self):
        from btcbot.news.scoring import score_sentiment

        assert score_sentiment("SEC has not approved the Bitcoin ETF") <= 0

    def test_sentiment_is_bounded(self):
        from btcbot.news.scoring import score_sentiment

        extreme = "surge rally soar gain jump record high approval adoption boost breakthrough"
        assert -1.0 <= score_sentiment(extreme) <= 1.0

    def test_high_impact_events_are_flagged(self):
        from btcbot.news.base import NewsCategory, NewsImpact
        from btcbot.news.scoring import score_impact

        assert score_impact(
            "FOMC rate decision due", 0.8, NewsCategory.FED_POLICY
        ) is NewsImpact.HIGH
        assert score_impact(
            "Minor altcoin listing news", 0.2, NewsCategory.OTHER
        ) is NewsImpact.LOW

    def test_cluster_id_is_a_stable_signature(self):
        """``cluster_id`` identifies a token set; matching is the deduplicator's job."""
        from btcbot.news.base import cluster_id

        headline = "SEC approves spot Bitcoin ETF applications"
        assert cluster_id(headline) == cluster_id(headline)
        # Word order and stopwords must not change the signature.
        assert cluster_id("Bitcoin ETF approved") == cluster_id("approved the Bitcoin ETF")
        assert cluster_id(headline) != cluster_id("Ethereum upgrade goes live on mainnet")

    def test_event_ids_are_stable(self):
        from btcbot.news.base import make_event_id

        when = now_utc()
        assert make_event_id("rss", "Bitcoin rallies", when) == make_event_id(
            "rss", "Bitcoin rallies", when
        )


class TestNewsPointInTime:
    def test_only_previously_received_news_is_visible(self, repos):
        """The look-ahead guard: filter on received time, not published time."""
        from btcbot.news.base import NewsCategory, NewsEvent, NewsImpact

        early = now_utc() - timedelta(hours=2)
        late = now_utc()

        for index, received in enumerate((early, late)):
            event = NewsEvent(
                event_id=f"news_{index}",
                provider="test",
                source="test",
                headline=f"Bitcoin headline {index}",
                published_ts=received,
                received_ts=received,
                btc_relevance=1.0,
                category=NewsCategory.BITCOIN,
                sentiment=0.5,
                impact=NewsImpact.HIGH,
                confidence=0.9,
            )
            repos.news.record(event.to_row())

        cutoff = iso(now_utc() - timedelta(hours=1))
        visible = repos.news.known_before(cutoff)
        assert len(visible) == 1
        assert visible[0]["event_id"] == "news_0"

    def test_news_engine_degrades_without_providers(self, repos):
        from btcbot.news.engine import NewsEngine

        engine = NewsEngine(NewsConfig(providers=[]), repos.news, experiment_id="exp_1")
        assert engine.is_degraded()
        state = engine.state_at()
        assert state.degraded
        assert state.label == "degraded"
        # Degradation must not block trading outright.
        assert not state.blocks_entry

    def test_disabled_news_yields_zero_influence(self, repos):
        from btcbot.news.engine import NewsEngine

        engine = NewsEngine(NewsConfig(enabled=False), repos.news, experiment_id="exp_1")
        state = engine.state_at()
        assert state.influence == 0.0
        assert not state.blocks_entry

    def test_high_impact_event_raises_risk_and_pauses_entries(self, repos):
        from btcbot.news.base import NewsCategory, NewsEvent, NewsImpact
        from btcbot.news.engine import NewsEngine

        engine = NewsEngine(
            NewsConfig(providers=[], high_impact_pause_minutes=30),
            repos.news,
            experiment_id="exp_1",
        )
        event = NewsEvent(
            event_id="news_hot",
            provider="test",
            source="test",
            headline="Federal Reserve announces emergency rate decision",
            published_ts=now_utc() - timedelta(minutes=5),
            received_ts=now_utc() - timedelta(minutes=5),
            btc_relevance=1.0,
            category=NewsCategory.FED_POLICY,
            sentiment=-0.5,
            impact=NewsImpact.HIGH,
            confidence=0.95,
        )
        repos.news.record(event.to_row())

        state = engine.state_at()
        assert state.high_impact_events >= 1
        assert state.risk_level > 0.5
        assert state.blocks_entry
        assert state.size_factor < 1.0

    def test_news_never_produces_a_direction_instruction(self, repos):
        """Sentiment is a feature; it must not be a buy/sell command."""
        from btcbot.news.engine import NewsState

        state = NewsState(directional_bias=0.9)
        assert not hasattr(state, "direction")
        assert not hasattr(state, "should_buy")
        assert -1.0 <= state.directional_bias <= 1.0

    def test_effectiveness_reduces_influence_when_gating_hurts(self, repos):
        from btcbot.news.engine import NewsEngine

        engine = NewsEngine(
            NewsConfig(providers=[], adaptive_influence=True), repos.news, experiment_id="exp_1"
        )
        before = engine.influence
        # Trades taken during elevated news did clearly better than calm ones.
        trades = [
            {"news_state": "elevated", "r_multiple": 1.5} for _ in range(20)
        ] + [{"news_state": "calm", "r_multiple": 0.1} for _ in range(20)]
        record = engine.evaluate_effectiveness(trades)
        assert record is not None
        assert record["decision"] == "reduced"
        assert engine.influence < before

    def test_effectiveness_needs_a_meaningful_sample(self, repos):
        from btcbot.news.engine import NewsEngine

        engine = NewsEngine(NewsConfig(providers=[]), repos.news, experiment_id="exp_1")
        assert engine.evaluate_effectiveness([{"news_state": "calm", "r_multiple": 1.0}]) is None


class TestMacroCalendar:
    def test_nonfarm_payrolls_lands_on_a_friday(self):
        from datetime import UTC, datetime

        from btcbot.news.providers.macro_calendar import MacroCalendarProvider

        provider = MacroCalendarProvider()
        events = provider.upcoming_events(reference=datetime(2026, 3, 1, tzinfo=UTC))
        payrolls = [e for e in events if "Payrolls" in e.name]
        for event in payrolls:
            assert event.when.weekday() == 4, "payrolls must fall on a Friday"
            assert (event.when.hour, event.when.minute) == (12, 30)

    def test_approximate_fomc_carries_lower_confidence(self):
        from datetime import UTC, datetime

        from btcbot.news.providers.macro_calendar import MacroCalendarProvider

        provider = MacroCalendarProvider()
        events = provider.upcoming_events(reference=datetime(2026, 3, 10, tzinfo=UTC))
        fomc = [e for e in events if "FOMC" in e.name]
        payrolls = [e for e in events if "Payrolls" in e.name]
        for event in fomc:
            assert event.confidence < 0.6, "modelled FOMC dates must be low-confidence"
        for event in payrolls:
            assert event.confidence >= 0.8

    def test_events_stay_inside_the_lookahead_window(self):
        from btcbot.news.providers.macro_calendar import MacroCalendarProvider

        provider = MacroCalendarProvider()
        for event in provider.upcoming_events():
            assert event.minutes_until <= provider.LOOKAHEAD_HOURS * 60 + 1


class TestHeadlineDeduplication:
    """Similarity-based clustering, since no two outlets phrase a story alike."""

    def test_reworded_headline_joins_the_same_cluster(self):
        from btcbot.news.base import HeadlineDeduplicator

        dedup = HeadlineDeduplicator()
        first_id, first_new = dedup.resolve("SEC approves spot Bitcoin ETF applications")
        second_id, second_new = dedup.resolve("Bitcoin ETF applications approved by the SEC")

        assert first_new is True
        assert second_new is False, "a reworded duplicate was treated as a new story"
        assert first_id == second_id

    def test_different_stories_get_different_clusters(self):
        from btcbot.news.base import HeadlineDeduplicator

        dedup = HeadlineDeduplicator()
        etf_id, _ = dedup.resolve("SEC approves spot Bitcoin ETF applications")
        hack_id, is_new = dedup.resolve("Major exchange hacked, 200 million dollars stolen")
        assert is_new is True
        assert etf_id != hack_id

    def test_stemming_collapses_word_forms(self):
        from btcbot.news.base import headline_tokens

        assert headline_tokens("approves") == headline_tokens("approved")
        assert headline_tokens("regulations") == headline_tokens("regulation")

    def test_similarity_is_bounded_and_symmetric(self):
        from btcbot.news.base import headline_tokens, similarity

        a = headline_tokens("Bitcoin rallies on ETF inflows")
        b = headline_tokens("ETF inflows drive Bitcoin rally")
        assert 0.0 <= similarity(a, b) <= 1.0
        assert similarity(a, b) == similarity(b, a)
        assert similarity(a, a) == 1.0
        assert similarity(a, frozenset()) == 0.0

    def test_cluster_memory_is_bounded(self):
        from btcbot.news.base import HeadlineDeduplicator

        dedup = HeadlineDeduplicator(capacity=25)
        for index in range(200):
            dedup.resolve(f"Completely unrelated distinct story number {index} alpha{index}")
        assert len(dedup) <= 25

    def test_empty_headline_is_handled(self):
        from btcbot.news.base import HeadlineDeduplicator

        cluster, is_new = HeadlineDeduplicator().resolve("")
        assert cluster.startswith("clu_")
        assert is_new is True
