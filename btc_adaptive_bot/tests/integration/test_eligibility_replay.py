"""The replay must be read-only and must not flatter either profile.

This is the tool that decides whether the balanced profile gets adopted, so
the ways it could mislead matter more than the ways it could crash:

* it must never write to the database it is judging
* a candidate with no closed shadow trade must not be counted as a winner
* the PnL must be charged at the real fee rate, not the shadow engine's
* the verdict must refuse to recommend a profile that is not net positive
"""

from __future__ import annotations

import sqlite3

import pytest

from btcbot.analysis.eligibility_replay import (
    ReplayResult,
    ReplayTrade,
    format_report,
    replay,
)
from btcbot.database.db import Database
from btcbot.database.migrations import run_migrations
from btcbot.execution.eligibility import TradeCosts
from btcbot.utils.timeutil import iso, now_utc

pytestmark = pytest.mark.integration

ENTRY = 118_000.0
TAKER = 0.0025
ROUND_TRIP = 0.0056


def seed(path, rows):
    """rows: (strategy, timeframe, stop_pct, target_pct, hit, hours_ago)."""
    with Database(str(path)) as db:
        run_migrations(db)
    con = sqlite3.connect(str(path))
    signal_cols = [r[1] for r in con.execute("PRAGMA table_info(signals)")]
    trade_cols = [r[1] for r in con.execute("PRAGMA table_info(shadow_trades)")]
    now = now_utc()
    from datetime import timedelta

    for i, (sid, tf, stop, target, hit, hours_ago) in enumerate(rows, start=1):
        ts = iso(now - timedelta(hours=hours_ago))
        sig = {
            "signal_id": f"sig_{i}", "experiment_id": "exp_t", "setup_id": f"su_{i}",
            "strategy_id": sid, "strategy_version": "1.0", "ts_utc": ts,
            "bar_open_ms": 1_700_000_000_000 + i, "symbol": "BTC-USDT-SWAP",
            "timeframe": tf, "direction": "long", "regime": "TREND_UP",
            "regime_confidence": 0.7, "entry_reference": ENTRY,
            "stop_price": ENTRY * (1 - stop), "target_price": ENTRY * (1 + target),
            "confidence": 0.7, "rr_ratio": target / stop, "accepted": 1,
            "rejection_reason": None, "routed_to": "shadow", "market_features": None,
            "news_features": None, "explanation": None, "created_at": ts,
        }
        con.execute(
            f"INSERT INTO signals ({','.join(signal_cols)}) "
            f"VALUES ({','.join('?' * len(signal_cols))})",
            tuple(sig.get(c) for c in signal_cols),
        )
        if hit is None:            # still open: no outcome row at all
            continue
        move = target if hit else -stop
        trade = {
            "trade_id": f"tr_{i}", "experiment_id": "exp_t", "signal_id": f"sig_{i}",
            "setup_id": f"su_{i}", "strategy_id": sid, "strategy_version": "1.0",
            "symbol": "BTC-USDT-SWAP", "timeframe": tf, "direction": "long",
            "sizing_model": "fixed_fractional", "entry_ts_utc": ts, "exit_ts_utc": ts,
            "entry_price": ENTRY, "exit_price": ENTRY * (1 + move),
            "stop_price": ENTRY * (1 - stop), "target_price": ENTRY * (1 + target),
            "quantity": 0.01, "notional": 1180.0, "fees": 0.0, "slippage_cost": 0.0,
            "spread_cost": 0.0, "pnl": move * 1180.0, "pnl_pct": move,
            "r_multiple": move / stop, "mfe": 0.0, "mae": 0.0, "duration_seconds": 60,
            "exit_reason": "take_profit" if hit else "stop_loss",
            "entry_regime": "TREND_UP", "exit_regime": "TREND_UP", "news_state": None,
            "confidence": 0.7, "is_open": 0, "created_at": ts,
        }
        con.execute(
            f"INSERT INTO shadow_trades ({','.join(trade_cols)}) "
            f"VALUES ({','.join('?' * len(trade_cols))})",
            tuple(trade.get(c) for c in trade_cols),
        )
    con.commit()
    con.close()


def run(path, **kwargs):
    return replay(str(path), hours=24, taker_fee_rate=TAKER, spread_bps=2.0,
                  equity=10_000.0, **kwargs)


WIDE_WINNER = ("ema_adx_trend_1h", "60", 0.006, 0.035, True, 2)
WIDE_LOSER = ("ema_adx_trend_1h", "60", 0.006, 0.035, False, 3)
MICRO = ("trade_flow_imbalance_1m", "1", 0.0004, 0.0008, True, 1)


class TestTheReplayIsReadOnly:
    def test_it_does_not_write_to_the_database(self, tmp_path):
        path = tmp_path / "ro.db"
        seed(path, [WIDE_WINNER, MICRO])
        before = path.stat().st_mtime_ns, path.read_bytes()

        run(path)

        after = path.stat().st_mtime_ns, path.read_bytes()
        assert before[1] == after[1], "the replay modified the database"

    def test_it_opens_the_file_in_read_only_mode(self):
        import inspect

        from btcbot.analysis import eligibility_replay

        source = inspect.getsource(eligibility_replay.replay)
        assert "mode=ro" in source


class TestItDoesNotFlatterAProfile:
    def test_an_unresolved_candidate_contributes_no_pnl(self, tmp_path):
        """Still-open trades are counted and then excluded from PnL."""
        path = tmp_path / "open.db"
        seed(path, [("ema_adx_trend_1h", "60", 0.006, 0.035, None, 2)])

        result = run(path)["C_feesafe_rr120"]

        assert result.candidates == 1
        assert result.unresolved == 1
        assert result.sent == 0
        assert result.net_pnl == 0.0
        assert result.winners == 0

    def test_fees_are_charged_at_the_real_rate_not_the_shadow_engines(self, tmp_path):
        path = tmp_path / "fees.db"
        seed(path, [WIDE_WINNER])

        trade = run(path)["C_feesafe_rr120"].trades[0]

        # The seeded shadow row recorded zero fees. The replay must ignore that
        # and charge the full round trip on the actual-trade notional.
        assert trade.fees == pytest.approx(ROUND_TRIP * trade.notional, rel=1e-6)
        assert trade.net_pnl == pytest.approx(trade.gross_pnl - trade.fees, rel=1e-9)
        assert trade.fees > 0

    def test_the_position_size_comes_from_the_risk_rule(self, tmp_path):
        path = tmp_path / "size.db"
        seed(path, [WIDE_WINNER])

        trade = run(path)["C_feesafe_rr120"].trades[0]

        # risk budget / stop distance, the essence of the sizing rule.
        assert trade.notional == pytest.approx(10_000.0 * 0.0075 / 0.006, rel=1e-6)

    def test_a_losing_window_is_reported_as_not_adoptable(self, tmp_path):
        path = tmp_path / "loss.db"
        seed(path, [WIDE_LOSER, WIDE_LOSER, WIDE_LOSER])

        result = run(path)["C_feesafe_rr120"]

        assert result.sent == 3
        assert result.net_pnl < 0
        assert not result.profitable
        report = format_report(
            run(path), hours=24,
            costs=TradeCosts(taker_fee_rate=TAKER, spread_bps=2.0,
                             slippage_bps=2.0, source="exchange"),
        )
        assert "NO profile is net positive" in report

    def test_an_empty_window_is_not_adoptable_either(self, tmp_path):
        path = tmp_path / "empty.db"
        seed(path, [MICRO])          # micro setups are all shadow-only

        results = run(path)
        assert results["C_feesafe_rr120"].sent == 0
        assert not results["C_feesafe_rr120"].profitable

        report = format_report(
            results, hours=24,
            costs=TradeCosts(taker_fee_rate=TAKER, spread_bps=2.0,
                             slippage_bps=2.0, source="exchange"),
        )
        assert "NO profile is net positive" in report
        assert "Adopt none of them" in report


class TestTheWindowAndTheProfiles:
    def test_signals_outside_the_window_are_excluded(self, tmp_path):
        path = tmp_path / "window.db"
        seed(path, [WIDE_WINNER, ("ema_adx_trend_1h", "60", 0.006, 0.035, True, 40)])

        result = run(path)["C_feesafe_rr120"]

        assert result.signals_seen == 1, "a signal older than 24h was replayed"

    def test_all_three_profiles_are_reported(self, tmp_path):
        """A = live config, B = fee-safe RR 1.00, C = fee-safe RR 1.20."""
        path = tmp_path / "both.db"
        seed(path, [WIDE_WINNER, MICRO])

        results = run(path)

        assert set(results) == {"A_live", "B_feesafe_rr100", "C_feesafe_rr120"}
        assert all(r.signals_seen == 2 for r in results.values())

    def test_micro_setups_are_shadow_only_under_every_profile(self, tmp_path):
        path = tmp_path / "micro.db"
        seed(path, [MICRO, MICRO, MICRO])

        for result in run(path).values():
            assert result.candidates == 0
            assert result.shadow_only == 3

    def test_a_lower_reward_risk_floor_never_takes_fewer_candidates(self, tmp_path):
        """B relaxes only RR, so it cannot refuse anything C accepted."""
        path = tmp_path / "mono.db"
        seed(path, [
            WIDE_WINNER, WIDE_LOSER, MICRO,
            ("donchian_breakout_15m", "15", 0.0008, 0.0146, True, 4),
            ("ema_trend_cross_15m", "15", 0.003, 0.02, False, 5),
        ])

        results = run(path)
        assert (
            results["B_feesafe_rr100"].candidates
            >= results["C_feesafe_rr120"].candidates
        )

    def test_the_reward_risk_floor_is_the_only_difference_between_b_and_c(self):
        from btcbot.execution.eligibility import FEE_SAFE_RR_100, FEE_SAFE_RR_120

        differing = {
            k for k in FEE_SAFE_RR_100 if FEE_SAFE_RR_100[k] != FEE_SAFE_RR_120[k]
        }
        assert differing == {"min_net_reward_risk"}


class TestReportShape:
    def test_the_summary_carries_every_requested_figure(self, tmp_path):
        path = tmp_path / "shape.db"
        seed(path, [WIDE_WINNER, WIDE_LOSER, MICRO])

        data = run(path)["C_feesafe_rr120"].as_dict()

        for key in (
            "actual_candidates", "trades_sent", "gross_pnl", "fees", "net_pnl",
            "by_timeframe", "by_strategy", "shadow_only",
        ):
            assert key in data, key

    def test_an_empty_result_is_safe_to_summarise(self):
        result = ReplayResult(label="balanced")
        data = result.as_dict()
        assert data["net_pnl"] == 0
        assert not result.profitable

    def test_an_unresolved_trade_reports_itself_as_such(self):
        trade = ReplayTrade(
            strategy_id="s", timeframe="15", direction="long", entry_price=ENTRY,
            stop_pct=0.005, target_pct=0.02, net_reward_risk=1.5, target_to_cost=3.0,
        )
        assert not trade.resolved
