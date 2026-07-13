"""Memcoin Trader CLI.

  python3 -m memetrader backtest [--hours 48] [--seed 7] [--verbose]
      Paper-trade the multi-agent stack on the offline market simulator.

  python3 -m memetrader evolve [--generations 6] [--population 10]
      Strategy Lab: evolve the parameter set; champion -> data/best_params.json.

  python3 -m memetrader paper [--minutes 60] [--interval 30]
      LIVE paper trading against real pump.fun / DexScreener / RugCheck data.
      No wallet, no keys, no real money — real market, fake fills.

  python3 -m memetrader report
      Show the saved paper portfolio's performance.

This project never signs or sends a transaction. Live execution is a
deliberate non-feature: graduate from paper only when the numbers earn it,
and even then start with money you can lose entirely.
"""
from __future__ import annotations

import argparse
import time

from .agents.strategy_lab import StrategyLab
from .config import BEST_PARAMS_PATH, SCALP_PARAMS_PATH, StrategyParams, load_scalp
from .engine.orchestrator import Orchestrator
from .engine.portfolio import Portfolio


def _print_report(pf: Portfolio) -> None:
    s = pf.stats()
    print("\n================ PERFORMANCE ================")
    print(f"  final equity     ${s['final_equity']:>12,.2f}")
    print(f"  return           {s['return_pct']:>+12.1f}%")
    print(f"  total pnl        ${s['total_pnl_usd']:>+12,.2f}")
    print(f"  trades           {s['trades']:>13}")
    print(f"  win rate         {s['win_rate']:>12.0%}")
    pf_str = f"{s['profit_factor']:.2f}" if s['profit_factor'] != float('inf') else "inf"
    print(f"  profit factor    {pf_str:>13}")
    print(f"  max drawdown     {s['max_drawdown_pct']:>12.1f}%")
    print(f"  avg hold         {s['avg_hold_minutes']:>10.0f} min")
    print(f"  best trade       {s['best_trade']:>13}")
    per = pf.per_strategy_stats()
    if per:
        print("\n---------------- by strategy ----------------")
        for name, st in sorted(per.items(), key=lambda kv: -kv[1]["pnl_usd"]):
            print(f"  {name:<18} trades {st['trades']:>4}  "
                  f"win {st['win_rate']:>4.0%}  "
                  f"avg {st['avg_multiple']:>5.2f}x  "
                  f"pnl ${st['pnl_usd']:>+10,.2f}")
    print("=============================================\n")


def cmd_backtest(args: argparse.Namespace) -> None:
    from .datafeed.simulator import SimMarket
    params = load_scalp() if args.scalp else StrategyParams.load()
    if args.bankroll:
        params.risk.starting_bankroll_usd = args.bankroll
    market = SimMarket(seed=args.seed)
    orch = Orchestrator(params, market, verbose=args.verbose)
    ticks = int(args.hours * 60)
    print(f"backtesting {args.hours}h of simulated memecoin market "
          f"(seed {args.seed}, bankroll ${params.risk.starting_bankroll_usd:,.0f})...")
    t0 = time.time()
    for i in range(ticks):
        market.step()
        orch.tick(market.now_ts)
        if args.verbose and i % 360 == 0 and i > 0:
            eq = orch.portfolio.equity_curve[-1]
            print(f"-- hour {i // 60}: equity ${eq:,.2f}, "
                  f"open positions {len(orch.portfolio.positions)}")
    print(f"done in {time.time() - t0:.1f}s "
          f"({len(market.tokens)} tokens simulated)")
    _print_report(orch.portfolio)


def cmd_evolve(args: argparse.Namespace) -> None:
    mode = "scalp (quick-flip)" if args.scalp else "swing"
    print(f"Strategy Lab: evolving {mode} parameters on the market simulator...")
    base = load_scalp() if args.scalp else StrategyParams.load()
    lab = StrategyLab(base=base, seed=args.seed)
    lab.evolve(generations=args.generations, population=args.population,
               ticks=int(args.hours * 60),
               save_path=SCALP_PARAMS_PATH if args.scalp else BEST_PARAMS_PATH)


def cmd_campaign(args: argparse.Namespace) -> None:
    from .campaign import reset_campaign, run_campaign_day, summary
    if args.reset:
        reset_campaign()
        print(f"campaign reset — starting fresh from ${args.bankroll:,.2f} "
              f"(old ledger archived)")
    row = run_campaign_day(bankroll=args.bankroll, hours=args.hours,
                           verbose=args.verbose)
    if row.get("already_ran"):
        print(f"already traded today ({row['date']} = day {row['day']}) — "
              f"next trading day runs tomorrow")
    print(f"day {row['day']} ({row['date']}): "
          f"${row['start_equity']:,.2f} -> ${row['end_equity']:,.2f} "
          f"({row['pnl_pct']:+.1f}%) | {row['trades']} trades, "
          f"win {row['win_rate']}%, avg hold {row['avg_hold_minutes']}m")
    print(summary())


def cmd_paper(args: argparse.Namespace) -> None:
    from .datafeed.live import LiveFeed
    params = load_scalp() if args.scalp else StrategyParams.load()
    if args.bankroll:
        params.risk.starting_bankroll_usd = args.bankroll
    pf = Portfolio.load() or Portfolio(params.risk.starting_bankroll_usd)
    orch = Orchestrator(params, LiveFeed(), portfolio=pf, verbose=True)
    end = time.time() + args.minutes * 60
    print(f"LIVE paper trading for {args.minutes} min — positions watched "
          f"every {args.fast_interval}s, discovery every {args.interval}s. "
          f"Ctrl-C to stop; state is saved.")
    next_discovery = 0.0
    import datetime as _dt
    from .engine.day_guard import DayGuard
    guard_date = _dt.date.today()
    guard = DayGuard(params.risk, pf.equity({}))
    try:
        while time.time() < end:
            now = time.time()
            if _dt.date.today() != guard_date:   # new day, fresh guard
                guard_date = _dt.date.today()
                guard = DayGuard(params.risk, pf.equity_curve[-1])
                print(f"-- new trading day, day guard reset "
                      f"(start ${guard.start:,.2f})")
            # fast clock: every open position, every few seconds — memecoins
            # go 2k -> 400k mcap (and back) in the blink of an eye
            if orch.manage_positions(now):
                pf.save()
            if guard.check(pf.equity_curve[-1]):
                if pf.positions:
                    orch.liquidate_all(now, guard.reason)
                    pf.save()
                    print(f"-- DAY GUARD [{guard.reason}]: all positions "
                          f"closed, no new trades until tomorrow")
            # slow clock: hunt for new entries (paused while guard is up)
            elif now >= next_discovery:
                orch.news.update()
                orch.discover_and_enter(now)
                next_discovery = now + args.interval
                pf.save()
            time.sleep(args.fast_interval)
    except KeyboardInterrupt:
        pass
    pf.save()
    _print_report(pf)


def cmd_study(_args: argparse.Namespace) -> None:
    from .study import run_study
    run_study()


def cmd_report(_args: argparse.Namespace) -> None:
    pf = Portfolio.load()
    if pf is None:
        print("no saved paper portfolio yet — run `python3 -m memetrader paper` first")
        return
    _print_report(pf)


def main() -> None:
    ap = argparse.ArgumentParser(prog="memetrader", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)

    b = sub.add_parser("backtest", help="paper trade on the offline simulator")
    b.add_argument("--hours", type=float, default=48.0)
    b.add_argument("--seed", type=int, default=7)
    b.add_argument("--scalp", action="store_true", help="quick-flip profile")
    b.add_argument("--bankroll", type=float, default=0.0)
    b.add_argument("--verbose", action="store_true")
    b.set_defaults(fn=cmd_backtest)

    e = sub.add_parser("evolve", help="Strategy Lab: evolve better parameters")
    e.add_argument("--generations", type=int, default=6)
    e.add_argument("--population", type=int, default=10)
    e.add_argument("--hours", type=float, default=24.0)
    e.add_argument("--seed", type=int, default=42)
    e.add_argument("--scalp", action="store_true",
                   help="evolve the quick-flip profile (saved separately)")
    e.set_defaults(fn=cmd_evolve)

    c = sub.add_parser("campaign", help="run one day of the persistent $20 "
                                        "quick-flip paper campaign")
    c.add_argument("--hours", type=float, default=24.0)
    c.add_argument("--bankroll", type=float, default=20.0)
    c.add_argument("--reset", action="store_true",
                   help="archive the ledger and restart from the bankroll")
    c.add_argument("--verbose", action="store_true")
    c.set_defaults(fn=cmd_campaign)

    p = sub.add_parser("paper", help="live paper trading on real market data")
    p.add_argument("--minutes", type=float, default=60.0)
    p.add_argument("--interval", type=float, default=45.0,
                   help="seconds between discovery sweeps")
    p.add_argument("--fast-interval", type=float, default=5.0, dest="fast_interval",
                   help="seconds between open-position checks (the fast clock)")
    p.add_argument("--scalp", action="store_true", help="quick-flip profile")
    p.add_argument("--bankroll", type=float, default=0.0)
    p.set_defaults(fn=cmd_paper)

    s = sub.add_parser("study", help="research the REAL market: sample live "
                                     "launches, mine the hot narrative vocabulary")
    s.set_defaults(fn=cmd_study)

    r = sub.add_parser("report", help="show saved paper portfolio performance")
    r.set_defaults(fn=cmd_report)

    args = ap.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
