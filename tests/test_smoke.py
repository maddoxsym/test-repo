"""Smoke tests: python3 -m unittest discover -s tests -v"""
import unittest

from memetrader.agents.rug_checker import RugChecker
from memetrader.config import StrategyParams
from memetrader.datafeed.simulator import SimMarket
from memetrader.engine.orchestrator import Orchestrator
from memetrader.engine.paper_broker import PaperBroker
from memetrader.models import TokenSnapshot, TokenSource


def _snap(**over) -> TokenSnapshot:
    base = dict(
        address="X", symbol="TST", name="test", source=TokenSource.SIM,
        created_at=0.0, price=1e-5, market_cap=50_000.0, liquidity=20_000.0,
        volume_5m=5_000.0, volume_1h=30_000.0, holders=150,
        top10_holder_pct=0.2, deployer_holds_pct=0.01, mint_revoked=True,
        freeze_revoked=True, lp_locked_or_burned=True, has_socials=True,
        bonding_progress=1.0, graduated=True, smart_wallet_buys_30m=0,
        price_high=1e-5, ts=600.0,
    )
    base.update(over)
    return TokenSnapshot(**base)


class TestRugChecker(unittest.TestCase):
    def test_clean_token_passes(self):
        rep = RugChecker(StrategyParams()).check(_snap())
        self.assertTrue(rep.passed)

    def test_unrevoked_mint_and_lp_fails(self):
        rep = RugChecker(StrategyParams()).check(
            _snap(mint_revoked=False, lp_locked_or_burned=False))
        self.assertFalse(rep.passed)

    def test_concentrated_holders_penalized(self):
        clean = RugChecker(StrategyParams()).check(_snap())
        conc = RugChecker(StrategyParams()).check(_snap(top10_holder_pct=0.6))
        self.assertLess(conc.score, clean.score)


class TestBroker(unittest.TestCase):
    def test_round_trip_costs_money(self):
        b = PaperBroker()
        t = _snap()
        tokens, spent = b.buy(t, 100.0)
        back = b.sell(t, tokens)
        self.assertLess(back, spent)          # fees + slippage
        self.assertGreater(back, spent * 0.9) # but not absurdly so at this size

    def test_big_order_worse_price(self):
        b = PaperBroker()
        t = _snap(liquidity=10_000.0)
        small, _ = b.buy(t, 10.0)
        big, _ = b.buy(t, 5_000.0)
        self.assertLess(big / 5_000.0, small / 10.0)  # tokens per USD degrade


class TestDayGuard(unittest.TestCase):
    def test_loss_stop(self):
        from memetrader.engine.day_guard import DayGuard
        from memetrader.config import RiskParams
        g = DayGuard(RiskParams(), 100.0)
        self.assertFalse(g.check(95.0))       # small dip: keep trading
        self.assertTrue(g.check(89.0))        # -11% -> daily loss stop
        self.assertEqual(g.reason, "daily_loss_stop")

    def test_profit_lock_banks_green_day(self):
        from memetrader.engine.day_guard import DayGuard
        from memetrader.config import RiskParams
        g = DayGuard(RiskParams(), 100.0)
        self.assertFalse(g.check(160.0))      # +60% arms the lock, floor 130
        self.assertFalse(g.check(140.0))      # giveback above floor: fine
        self.assertTrue(g.check(129.0))       # floor touched -> bank the day
        self.assertEqual(g.reason, "profit_lock")
        # halted day ends ABOVE where it started: profit protected
        self.assertGreater(129.0, g.start)

    def test_ratchets_with_new_peak(self):
        from memetrader.engine.day_guard import DayGuard
        from memetrader.config import RiskParams
        g = DayGuard(RiskParams(), 100.0)
        g.check(150.0)
        floor1 = g.floor()
        g.check(300.0)                        # new peak -> floor rises
        self.assertGreater(g.floor(), floor1)


class TestEndToEnd(unittest.TestCase):
    def test_backtest_runs_and_trades(self):
        market = SimMarket(seed=5)
        orch = Orchestrator(StrategyParams(), market)
        for _ in range(1440):  # 24 sim-hours
            market.step()
            orch.tick(market.now_ts)
        stats = orch.portfolio.stats()
        self.assertGreater(stats["trades"], 0)
        self.assertGreater(stats["final_equity"], 0)

    def test_params_roundtrip(self):
        p = StrategyParams()
        q = StrategyParams.from_dict(p.to_dict())
        self.assertEqual(p.to_dict(), q.to_dict())


if __name__ == "__main__":
    unittest.main()
